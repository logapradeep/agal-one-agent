"""ProgramHost — the node runs the bundle the cloud serves, whatever its language (contracts v1.15.0, ADR-029
decision 12; programs/README.md §7.2 "Coexistence").

The agent reports ``capabilities.blockLanguage = 3``. The cloud serves a v3 bundle only to a node that reports
3 AND whose v1 NAB is migrated (``/nodePrograms/{nodeUid}.migratedAt``, or no NAB); every other node keeps its
language-1 NAB bundle — including this one until its migration. So the host holds both runtimes and runs
the bundle it is given:

* a language-1 bundle (``language`` absent or 1) runs on :class:`~.runtime.BlockRuntime`, exactly as agent
  0.3.4 — the release this one is built on — runs it (tests/test_v1_parity.py compares the two whole agents);
* a language-3 bundle runs on :class:`~.v3.runtime.RuntimeV3`;
* language 2 was superseded before it was built: it is rejected.

A bundle of the language that runs takes over from the running program (each runtime's own bumpless swap).
A bundle of the other language — the migration (v1 → v3), done once per node with the founder's yes, or its
rollback (v3 → v1) — changes language BUMPLESSLY too (contracts v1.15.1, programs/README.md §7.5a):

* it is checked first: a bundle that does not compile is rejected and the running program stays;
* it waits for a moment with no run in progress (a v1 schedule or plot run, a v3 run, an end still due, an output
  kept to the end of its run, automation paused, a command not applied yet): while a runtime thread runs passes,
  the decision is made at the end of one of its passes, on that thread, under the lock, and that runtime runs no
  pass after it — so no pass can start a run between the check and the switch;
* the old runtime stops WITHOUT its clean-stop safe state, and the new one starts with every output the two share
  at the level the old one left (the same card and port, the same line driven the same way; a continuous valve's
  ``valve.open`` from its v1 coil, and back) — nothing is written for it; a line the new program does not drive is
  switched off and let go of, as at any reload (``output_released``);
* it is not a boot (no ``boot`` event, no runs carried), and a schedule id that ran or was missed today keeps that.

Contracts v1.15.2 (§7.5a; the review of 0.4.0 on the Pi's shape and on a synthetic farm):

* **one runtime at a time** — while the host switches it counts as running (``_running``) and honours no
  :meth:`start`, so main.py's once-a-second "a program that arrived after boot starts the runtime" check can no
  longer start the OLD runtime in the middle of a switch (both languages then ran passes until the process exited);
* **a latched E-stop at the switch itself** — the hold waits while the E-stop is latched (a latch that comes in the
  pass that ends the hold included), and the switch checks it once more before it stops the v3 runtime;
* **only what runs is stored** — the host calls :attr:`on_switched` with the bundle that runs after a switch; a held
  bundle is never the stored program (blocks/sync.py).

The 0.4.0 review after v1.15.2 (the end-to-end runs found the old and the new runtime threads alive together for
25 ms to 4.7 s in every run: the switch ran ON the old runtime's thread, which started the new runtime and then waited
for the lock the new runtime's first pass held while it posted its programAck over HTTPS):

* **never two runtime threads, at any instant** — the old runtime's thread cannot wait for itself to end, so it only
  decides (and runs no pass after); a thread of its own, ``language-switch``, joins the old runtime's threads (the
  pass loop and the sampler, bounded by :data:`SWITCH_JOIN_S`) and only then starts the new runtime. A switch with no
  runtime thread running (a pull while the node is stopped, the tests' :meth:`step`) is made on the caller's thread;
* **nothing lands in the middle of a switch** — a command, a compile or a stop that arrives while the node changes
  language waits for it to end (the lock is let go of meanwhile) and then goes to the program that runs: before, a
  command accepted by the old program in that moment was queued on a runtime that ran no pass again, and was lost;
* a pass never waits on the network (blocks/cloud.py): the new runtime's first pass hands its ``programAck`` to the
  sink's outbox, so nothing holds the lock while the ingress answers.

Both runtimes share one lock, so a switch command is checked and dispatched against whichever program runs.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from .clock import Clock, SystemClock
from .io import IOAdapter, PortRef, physical_port
from .runtime import BlockRuntime, CompileError, EventSink
from .v3.runtime import RuntimeV3

logger = logging.getLogger(__name__)

BLOCK_LANGUAGE = 3  # what this agent reports as capabilities.blockLanguage (enums.yaml BlockLanguage)
# Why a held switch back to language 1 waits while the E-stop is latched (README §7.10, contracts v1.15.2).
ESTOP_HOLD = "the E-stop is latched at the panel"
# How long a switch waits for the old runtime's threads to end before it starts the new runtime. They end within one
# sampler period (100 ms) of the decision — the pass loop at once, the sampler after its sleep; a GPIO read that hangs
# could hold one longer. Past this the switch says so in the log and starts the new runtime anyway: the old runtime's
# loop checks its flag before every pass, so a thread left over can never begin another pass.
SWITCH_JOIN_S = 10.0
# How long a command, a compile or a stop waits for a switch in progress (the join, the take-over, the store).
SWITCH_WAIT_S = SWITCH_JOIN_S + 5.0
SWITCH_THREAD = "language-switch"
SWITCHING_REFUSAL = "refused: the node is changing its block language — send it again in a moment"


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
        # Waited on (the lock let go of meanwhile) by whatever arrives while the node changes language.
        self._switch_cond = threading.Condition(self._lock)
        self.v1 = BlockRuntime(io, sink, clock=self.clock, state_dir=state_dir, tick_seconds=tick_seconds,
                               lock=self._lock, listen=False)
        self.v3 = RuntimeV3(io, sink, clock=self.clock, state_dir=state_dir, tick_seconds=tick_seconds,
                            node_uid=node_uid, lock=self._lock, listen=False)
        self.active = None
        self._started = False
        # Counts the host's stops: a switch started before a stop does not start the new runtime after it.
        self._stops = 0
        # A checked bundle of the other language, held until a pass ends with no run in progress (§7.5a).
        self._pending: Optional[dict] = None
        self._pending_why: Optional[str] = None
        # True from the moment a switch of language begins until the new runtime runs (v1.15.2): the host then
        # counts as running and starts nothing, whoever asks.
        self._switching = False
        # Called with the bundle that runs after a switch of language — ProgramSync stores it as the node's program
        # then, never while it is only held (v1.15.2).
        self.on_switched: Optional[Callable[[dict], None]] = None
        self.v1.after_pass = self._after_pass
        self.v3.after_pass = self._after_pass
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
        # A switch of language in progress counts as running (v1.15.2): main.py's restart check must not start the
        # old runtime while it hands over.
        return self._switching or (self.active is not None and self.active._running)

    def _wait_for_switch_locked(self) -> bool:
        """Under self._lock: wait (letting go of the lock) while a switch of language is in progress; True when none
        is. Never on the switch's own thread."""
        if not self._switching or threading.current_thread().name == SWITCH_THREAD:
            return not self._switching
        return self._switch_cond.wait_for(lambda: not self._switching, SWITCH_WAIT_S)

    # ------------------------------------------------------------- compile

    @property
    def pending_version(self) -> Optional[int]:
        """The version of a bundle of the other language held for the switch, or None."""
        return int(self._pending.get("version", 0)) if self._pending is not None else None

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
        with self._lock:
            # Never in the middle of a switch: decided against the program that ran before it, a bundle could land on
            # the runtime the switch is stopping, or be overtaken by the switch's own (0.4.0 review).
            if not self._wait_for_switch_locked():
                raise CompileError("could not take over from the running program: the node is still changing its "
                                   "block language")
            if self.active is None or target is self.active:
                target.compile(bundle)
                self.active = target
                if self._pending is not None:
                    logger.info("program v%s of the running language replaces the held switch to v%s",
                                bundle.get("version"), self._pending.get("version"))
                    self._pending, self._pending_why = None, None
                return
            # The migration (v1 → v3), or back: check the new bundle first — a bundle that does not compile never
            # stops the running program.
            old = self.active
            if old is self.v3 and self.v3.prog is not None and self.v3.prog.estop is not None and self.v3.prog.estop.latched:
                # README §7.10: nothing takes the latch off — a language without the E-stop would drop it, and the
                # motors would restart with no reset at the panel.
                raise CompileError("refused: the E-stop is latched at the panel — the node does not leave block "
                                   "language 3 while it is latched; it clears only at the panel")
            if target is self.v3:
                from .v3 import program as P3
                prog3 = P3.build(bundle, self.io)  # raises CompileError: the running v1 program stays
                refusal = self.v3.latch_refusal(prog3)  # a latch the v3 runtime persisted before (a switch back and forth)
                if refusal:
                    raise CompileError(refusal)
            else:
                target._build(bundle)
            self._pending = bundle
            why = self._hold_reason()
            running = old._running
        if running or why:
            # A runtime thread runs passes: the switch is decided at the end of one of them, on that thread (no pass
            # can start a run between the check and the switch); a run in progress makes it wait longer.
            self._note_wait(bundle, lang, why or "the end of the running pass")
            return
        self._switch()

    def _note_wait(self, bundle: dict, lang: int, why: str) -> None:
        if why != self._pending_why:
            self._pending_why = why
            logger.warning("program v%s is block language %d: the node changes language between runs — it waits: %s",
                           bundle.get("version"), lang, why)

    def _leaving_latched(self) -> bool:
        """Does the held bundle leave language 3 while the E-stop is latched (README §7.10)? Under self._lock."""
        pending, prog = self._pending, self.v3.prog
        return (pending is not None and self.active is self.v3 and bundle_language(pending) != 3 and prog is not None
                and prog.estop is not None and bool(prog.estop.latched))

    def _hold_reason(self) -> Optional[str]:
        """Why the held switch waits at the end of this pass, or None (§7.5a): a run in progress, or — leaving
        language 3 — a latched E-stop (v1.15.2: a latch that came after the bundle was checked). Under self._lock."""
        why = self.active.busy_reason() if self.active is not None else None
        if why:
            return why
        return ESTOP_HOLD if self._leaving_latched() else None

    def _after_pass(self) -> None:
        """After a pass: make the held switch of language when no run is in progress (§7.5a). The running runtime's
        own thread calls this at the end of each of its passes (and :meth:`step` does, in the tests)."""
        if self._pending is None:
            return
        with self._lock:
            pending = self._pending
            if pending is None or self.active is None:
                return
            why = self._hold_reason()
            plan = None if why else self._begin_switch_locked()
        if why:
            self._note_wait(pending, bundle_language(pending), why)
            return
        if plan is None:
            return
        old = plan[1]
        if threading.current_thread() is old._thread:
            # The old runtime's own thread: it cannot wait for itself to end. It runs no pass after this one (its flag
            # is down) and ends when this hook returns; the switch is made by a thread of its own, which waits for it
            # — and for the old sampler — before it starts the new runtime (0.4.0 review: never two runtime threads).
            threading.Thread(target=self._finish_switch, args=plan, kwargs={"raise_errors": False},
                             name=SWITCH_THREAD, daemon=True).start()
        else:
            self._finish_switch(*plan, raise_errors=False)

    def _switch(self) -> None:
        """Change language now — no runtime thread runs passes (a pull while the node is stopped). From the first step
        to the new runtime's start the host counts as running and starts nothing (v1.15.2)."""
        with self._lock:
            plan = self._begin_switch_locked()
        if plan is not None:
            self._finish_switch(*plan, raise_errors=True)

    def _begin_switch_locked(self):
        """Under self._lock, at a moment with no run in progress: take the held bundle and stop the old runtime's
        passes — its loop runs none after this (the flag it checks before every pass is down). Returns what
        :meth:`_finish_switch` needs, or None (nothing held, or the E-stop latched)."""
        bundle = self._pending
        if bundle is None or self.active is None:
            return None
        if self._leaving_latched():
            # README §7.10 (v1.15.2): checked once more here, under the lock, before the v3 runtime stops — the node
            # does not leave language 3 while the E-stop is latched; the hold waits for the panel reset.
            self._note_wait(bundle, bundle_language(bundle), ESTOP_HOLD)
            return None
        old = self.active
        self._pending, self._pending_why = None, None
        self._switching = True
        was_running = old._running or self._started
        old._running = False
        old._wake.set()
        return bundle, old, was_running, self._stops

    def _finish_switch(self, bundle: dict, old, was_running: bool, stops: int, raise_errors: bool = True) -> None:
        version = bundle.get("version")
        try:
            self._switch_now(bundle, old, was_running, stops)
        except Exception as e:  # noqa: BLE001 — a switch that fails half-way: the old program is running again
            if raise_errors:
                raise
            logger.error("program v%s could not take over from the running program: %s", version, e, exc_info=True)
            try:
                # Through the sink's outbox: never a post on this thread (blocks/cloud.py).
                self.sink.program_ack(int(version or 0), "rejected", f"could not take over from the running program: {e}"[:500])
            except Exception:  # noqa: BLE001
                pass
        finally:
            with self._lock:
                self._switching = False
                self._switch_cond.notify_all()

    def _join_old(self, old) -> None:
        """Wait for the old runtime's threads — its pass loop and its sampler — to end (bounded: SWITCH_JOIN_S)."""
        me = threading.current_thread()
        deadline = time.monotonic() + SWITCH_JOIN_S
        for t in (old._thread, old._sampler):
            if t is None or t is me:
                continue
            t.join(max(0.0, deadline - time.monotonic()))
            if t.is_alive():
                logger.error("the old runtime's thread %s has not ended %.0f s after the switch began: the new runtime "
                             "starts anyway (the old one is stopped — it begins no pass)", t.name, SWITCH_JOIN_S)

    def _switch_now(self, bundle: dict, old, was_running: bool, stops: int) -> None:
        """The old runtime stops without its safe state and its threads end; then, under the lock, the new one takes
        every output it shares over at its level (programs/README.md §7.5a) and starts — its first pass waits for the
        lock, so it comes after the let-go of what it does not drive and after the store."""
        lang = bundle_language(bundle)
        target = self.v3 if lang == 3 else self.v1
        if old is self.v3:
            # Every output stays as the old program left it; joins its threads (3 s each). Its clean-stop record is
            # handed to the state file's writer without waiting for the fsync (README §4.8, the second 0.4.0 review):
            # it is read only at a later v3 load, which waits for it — so the node is one fsync less with no runtime.
            old.stop(safe_state=False, wait_record=False)
        else:
            old.stop(safe_state=False)  # every output stays as the old program left it; joins its threads (3 s each)
        self._join_old(old)
        with self._lock:
            start = was_running and self._stops == stops  # a stop that came meanwhile: load, but start nothing
            carried = old.driven_levels()
            days = old.schedule_days()
            old_bundle = old.bundle
            try:
                target.take_over_language(bundle, carried, days)
            except Exception:
                # Checked before, so this should not happen; if it does, the old program takes its own outputs back
                # at their levels and runs on.
                logger.error("the switch to language %d failed: the language-%d program runs on", lang,
                             3 if old is self.v3 else 1)
                if old_bundle is not None:
                    old.take_over_language(old_bundle, carried, days)
                if start:
                    old.start()
                raise
            self.active = target
            if start:
                target.start()  # its threads start now; its first pass waits for this lock
            # What the new program does not drive is let go of, switched off — as at any reload (README §4.8).
            now_lines = target.output_lines()
            for key, c in sorted(carried.items()):
                port = c["port"]
                if physical_port(port.transport) in now_lines:
                    continue
                try:
                    self.io.write_output(port, False if not isinstance(c["value"], float) else 0.0)
                    self.io.release_output(port)
                except Exception as e:  # noqa: BLE001
                    logger.error("letting go of %s of card %s failed: %s", key[1], key[0], e)
                    continue
                logger.info("output %s.%s is not in the language-%d program: switched off and let go of", key[0], key[1], lang)
                self.sink.event("output_released", {"assetId": key[0], "sourceKey": key[1], "level": "off"})
            logger.warning("program v%s: the node now runs block language %d (it took over without switching an output)",
                           bundle.get("version"), lang)
            if self.on_switched is not None:
                try:
                    self.on_switched(bundle)  # the node's stored program is now this one (blocks/sync.py)
                except Exception as e:  # noqa: BLE001 — never fail a switch that is made
                    logger.error("storing program v%s after the switch failed: %s", bundle.get("version"), e)
            # Done, in the same hold of the lock as the new runtime's start: the host never reads as stopped between.
            self._switching = False
            self._switch_cond.notify_all()

    # ------------------------------------------------------------- lifecycle

    def start(self) -> None:
        # v1.15.2: under the lock, and never while a switch is in progress — the switch starts the runtime that takes
        # over itself; starting the one read here could otherwise start the runtime the switch is stopping.
        with self._lock:
            if self._switching:
                return
            self._started = True
            if self.active is not None:
                self.active.start()

    def stop(self, safe_state: bool = True) -> None:
        with self._lock:
            self._started = False
            self._stops += 1
            # A switch in progress finishes first (it starts nothing after this stop), so the stop below reaches the
            # runtime that runs — and its clean stop records the node's state.
            if not self._wait_for_switch_locked():
                logger.error("stopping while the node is still changing its block language")
            active = self.active
        if active is not None:
            active.stop(safe_state=safe_state)

    def apply_safe_state(self) -> None:
        if self.active is not None:
            self.active.apply_safe_state()

    def step(self) -> None:
        if self.active is not None:
            self.active.step()
            self._after_pass()

    def _on_input_change(self) -> None:
        if self.active is not None:
            self.active._on_input_change()

    # ------------------------------------------------------------- commands

    def apply_command(self, command: dict) -> dict:
        with self._lock:
            # A command that arrives while the node changes language waits for it (a moment: the old runtime's
            # threads end, the new one takes over) and goes to the program that runs then, which checks it. Queued on
            # the old runtime, it would never be applied: that runtime runs no pass again (0.4.0 review).
            if not self._wait_for_switch_locked():
                return {"accepted": False, "reason": SWITCHING_REFUSAL}
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
        """(assetId, sourceKey) of the program's output on this chip line — a port test pulses it through the
        runtime. Language 1: the variable on that line. Language 3 (contracts v1.15.1): a plain output only — a
        motor's line (its guard supervises it) and a valve's coils (its driver's) are never handed to a test,
        which is refused as busy there."""
        if self.active is self.v3:
            return self.v3.plain_output_on_line(label, line) if self.v3.loaded else None
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

    def outputs_on(self) -> list[str]:
        """What the running program has ON now, named for a person — a restart switches these off, so an OTA
        update waits while this is not empty (main.outputs_on_for, ota_updater). A language-1 program answers
        exactly as agent 0.3.4 does."""
        if self.active is None or not self.active.loaded:
            return []
        return self.active.outputs_on()

    def status_extra(self) -> dict:
        return self.v3.status_extra() if self.active is self.v3 else {}

    def snapshot(self) -> dict:
        return self.runtime.snapshot()
