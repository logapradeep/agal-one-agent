"""Durable telemetry batch uploader — ADR-013 P0 (§5.2, §5.3).

Drains the on-node SQLite ring buffer (:mod:`agal_one_agent.telemetry_buffer`)
to the HTTPS ingress in ``telemetry_batch`` payloads, oldest-first within
priority, with exponential backoff + jitter on failure and an on-success prune.
Survives a network outage: readings sit durably in the buffer until a drain
lands an HTTP 200, at which point the rows are marked sent and pruned.

The uploader is deliberately transport-thin — it owns the *when* (flush window,
pending threshold, backoff) and delegates the *how* to
:meth:`HttpReporter.report_telemetry_batch`, which already carries the
``Authorization: Bearer`` header (daemon v0.1.7).
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Optional

from .telemetry_buffer import TelemetryBuffer, TelemetryBatchConfig
from .http_reporter import HttpReporter

logger = logging.getLogger(__name__)

#: Exponential backoff bounds shared with the reconnect logic (§5.3).
BACKOFF_BASE_SEC = 1.0
BACKOFF_CAP_SEC = 300.0   # 5-minute cap
#: Drain when this many rows are pending even before the flush window elapses
#: (>=500 pending rows ⇒ flush now, §5.2).
FLUSH_ON_PENDING = 500


class TelemetryUploader:
    """Background thread that drains the buffer to the ingress on a cadence."""

    def __init__(
        self,
        buffer: TelemetryBuffer,
        reporter: HttpReporter,
        config: Optional[TelemetryBatchConfig] = None,
    ) -> None:
        self.buffer = buffer
        self.reporter = reporter
        self.config = config or buffer.config
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._wake = threading.Event()
        self._backoff = BACKOFF_BASE_SEC

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="telemetry-uploader", daemon=True,
        )
        self._thread.start()
        logger.info(
            "Telemetry uploader started (flush=%ds, max_readings=%d)",
            self.config.flush_interval_sec, self.config.max_readings,
        )

    def stop(self) -> None:
        self._running = False
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def wake(self) -> None:
        """Nudge the uploader to drain now (e.g. on MQTT reconnect, §5.3)."""
        self._wake.set()

    # ---- Loop ---------------------------------------------------------------

    def _run(self) -> None:
        while self._running:
            # Sleep until the flush window elapses, we're woken, or (cheap poll)
            # the pending count crosses the threshold.
            self._wake.wait(timeout=self._sleep_seconds())
            self._wake.clear()
            if not self._running:
                break
            try:
                self.drain_all()
            except Exception as e:  # noqa: BLE001 — never let the thread die
                logger.error("Uploader drain error: %s", e)

    def _sleep_seconds(self) -> float:
        # If we're mid-backoff (last drain failed), honour the backoff; else the
        # normal flush window. A large pending backlog shortens the wait.
        if self._backoff > BACKOFF_BASE_SEC:
            return self._backoff
        if self.buffer.pending_count() >= FLUSH_ON_PENDING:
            return 0.1
        return float(self.config.flush_interval_sec)

    def drain_all(self) -> int:
        """Drain every pending batch until the buffer is empty or a send fails.

        Returns the number of readings successfully uploaded this call. On a
        failed send, applies exponential backoff with jitter and stops (rows
        stay pending for the next attempt).
        """
        total_sent = 0
        while self._running or total_sent == 0:
            batch = self.buffer.fetch_batch(max_readings=self.config.max_readings)
            if batch is None:
                self._reset_backoff()
                break
            ok = self.reporter.report_telemetry_batch(
                batch.batch_id, batch.readings,
                boot_session_id=batch.boot_session_id,
            )
            if not ok:
                self._apply_backoff()
                logger.warning(
                    "Batch %s (%d readings) upload failed; backing off %.1fs",
                    batch.batch_id, len(batch), self._backoff,
                )
                break
            self.buffer.mark_sent(batch.seqs)
            total_sent += len(batch)
            self._reset_backoff()
            logger.debug("Uploaded batch %s (%d readings)", batch.batch_id, len(batch))
            # Guard against an unbounded loop if fetch keeps returning full
            # batches faster than we drain — one full batch per iteration is
            # enough; loop continues while more remain.
            if len(batch) < self.config.max_readings:
                break
        return total_sent

    # ---- Backoff (§5.3: 1 s → 5 min cap, jittered) -------------------------

    def _apply_backoff(self) -> None:
        self._backoff = min(self._backoff * 2, BACKOFF_CAP_SEC)

    def _reset_backoff(self) -> None:
        self._backoff = BACKOFF_BASE_SEC

    @property
    def current_backoff(self) -> float:
        """Backoff with full jitter applied (exposed for the sleep + tests)."""
        return random.uniform(0, self._backoff)
