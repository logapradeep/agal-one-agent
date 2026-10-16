"""Program store + cloud sync for the block runtime (ADR-017 §5 transport).

* The cloud pokes the node over MQTT with ``syncProgram {version}``.
* The node pulls the compiled bundle over HTTPS (``getProgram``) — on that poke,
  on boot and on every MQTT reconnect — compiles it, persists it under the state
  directory, and acknowledges over the telemetry ingress (``programAck``).
* A rejected bundle leaves the previous program running.
* Contracts v1.15.2 (programs/README.md §7.5a): ``program.json`` is always the RUNNING bundle. A bundle of the other
  language that the host only holds for its switch of language is not stored: it is stored when the switch is made
  (``ProgramHost.on_switched``). A restart during the hold therefore boots the language that was running — which
  carries its interrupted run as after any restart — and the next pull hands the held bundle to it again.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from .runtime import CompileError

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/var/lib/agal-one-agent"


class ProgramStore:
    """Persists the last accepted bundle so the node boots offline with its program."""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "program.json")
        self._lock = threading.Lock()  # a pull and a switch of language may store at the same moment

    def load(self) -> Optional[dict]:
        try:
            with open(self.path) as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:  # noqa: BLE001
            logger.warning("program.json unreadable: %s", e)
            return None

    def save(self, bundle: dict) -> None:
        with self._lock:
            self._save(bundle)

    def _save(self, bundle: dict) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(bundle, f)
            os.replace(tmp, self.path)
        except Exception as e:  # noqa: BLE001
            logger.warning("program.json write failed: %s", e)


class ProgramSync:
    """Glue between the cloud (http_reporter) and the runtime."""

    def __init__(self, runtime, store: ProgramStore, http_reporter, sink_ack=None):
        # runtime: a ProgramHost (the agent) or a BlockRuntime (tests, the simulator)
        self.runtime = runtime
        self.store = store
        self.http = http_reporter
        self._sink_ack = sink_ack
        self._lock = threading.Lock()
        if hasattr(runtime, "on_switched"):
            # v1.15.2: a bundle of the other language is stored when the host switches to it, not when it is held.
            runtime.on_switched = self._stored_at_switch

    def _stored_at_switch(self, bundle: dict) -> None:
        # What runs at this moment, read under the store's lock: a pull that compiled a newer bundle of the new
        # language in the meantime stored that one, and is not overwritten with the older one.
        with self.store._lock:
            running = getattr(self.runtime, "bundle", None)
            running = running if isinstance(running, dict) else bundle
            self.store._save(running)
        logger.info("program v%s stored: the node now runs it", running.get("version"))

    def load_persisted(self) -> bool:
        bundle = self.store.load()
        if not bundle:
            return False
        try:
            self.runtime.compile(bundle)
            return True
        except CompileError as e:
            logger.error("persisted program rejected: %s", e)
            return False

    def pull(self, expected_version: Optional[int] = None) -> None:
        """Fetch the bundle if the cloud has a newer version; compile; ack. Non-blocking."""
        threading.Thread(target=self._pull, args=(expected_version,), name="program-pull", daemon=True).start()

    def _pull(self, expected_version: Optional[int] = None) -> None:
        with self._lock:
            current = self.runtime.version
            try:
                result = self.http.fetch_program(current_version=current)
            except Exception as e:  # noqa: BLE001
                logger.warning("getProgram failed: %s", e)
                return
            if not result:
                return
            if result.get("unchanged"):
                logger.info("program v%d unchanged", current)
                return
            bundle = result.get("bundle")
            if not isinstance(bundle, dict):
                logger.warning("getProgram returned no bundle")
                return
            version = int(bundle.get("version", 0))
            if expected_version and version < expected_version:
                logger.warning("getProgram returned v%d, cloud announced v%d", version, expected_version)
            try:
                self.runtime.compile(bundle)
                held = getattr(self.runtime, "pending_version", None)
                if held is not None and held == version:
                    # v1.15.2: held for the switch of language — the running program stays the stored one until the
                    # switch is made (on_switched stores this one then); a restart meanwhile pulls it again.
                    logger.info("program v%d held for the switch of language: the running program stays stored", version)
                else:
                    self.store.save(bundle)
                    logger.info("program v%d applied", version)
            except Exception as e:  # noqa: BLE001 — CompileError, or anything else: never a silent dead pull
                if not isinstance(e, CompileError):
                    logger.error("program v%d: compile failed unexpectedly", version, exc_info=True)
                logger.error("program v%d rejected: %s", version, e)
                self.http.report_program_ack(version, "rejected", reason=str(e))
                if self._sink_ack:
                    self._sink_ack(version, "rejected", str(e))
