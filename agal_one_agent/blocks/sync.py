"""Program store + cloud sync for the block runtime (ADR-017 §5 transport).

* The cloud pokes the node over MQTT with ``syncProgram {version}``.
* The node pulls the compiled bundle over HTTPS (``getProgram``) — on that poke,
  on boot and on every MQTT reconnect — compiles it, persists it under the state
  directory, and acknowledges over the telemetry ingress (``programAck``).
* A rejected bundle leaves the previous program running.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from .runtime import BlockRuntime, CompileError

logger = logging.getLogger(__name__)

DEFAULT_STATE_DIR = "/var/lib/agal-one-agent"


class ProgramStore:
    """Persists the last accepted bundle so the node boots offline with its program."""

    def __init__(self, state_dir: str = DEFAULT_STATE_DIR):
        self.state_dir = state_dir
        self.path = os.path.join(state_dir, "program.json")

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

    def __init__(self, runtime: BlockRuntime, store: ProgramStore, http_reporter, sink_ack=None):
        self.runtime = runtime
        self.store = store
        self.http = http_reporter
        self._sink_ack = sink_ack
        self._lock = threading.Lock()

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
                self.store.save(bundle)
                logger.info("program v%d applied", version)
            except CompileError as e:
                logger.error("program v%d rejected: %s", version, e)
                self.http.report_program_ack(version, "rejected", reason=str(e))
                if self._sink_ack:
                    self._sink_ack(version, "rejected", str(e))
