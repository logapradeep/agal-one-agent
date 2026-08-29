"""On-node durable telemetry ring buffer — ADR-013 P0.

Every telemetry reading is appended to a SQLite WAL database *first*, before the
live payload is published, so a network outage becomes a delay rather than a
hole in a *paid* history (ADR-013 §1.2 gap #1, §5.1).

Design (ADR-013 §5):

* SQLite WAL append log at ``/var/lib/agal-one-agent/telemetry.db``. WAL keeps SD
  card wear sane — one fsync per batch commit, not per row.
* Row schema mirrors the ADR verbatim:
  ``(seq INTEGER PK autoinc, ts_ms, ts_uncertain, port_key, kind_uri, value,
     unit, quality, win_json, sent, batch_id, priority, boot_session_id)``.
* Ring bounds: keep at least ``retention_hours`` (default 48 h) of the newest
  rows; hard caps at ``max_rows`` (7 days at 10 s × ~6 ports ≈ 360 k rows) and
  ``max_bytes`` (64 MB). On overflow we drop the oldest *unsent-or-sent* rows and
  emit a ``buffer_overflow`` sensor_event on the first drop of a run (§5.1).
* Draining: oldest-first, ``≤ max_readings`` (default 500) or ``≤ max_bytes``
  per batch, raw-tier ports first (priority column, §5.3). Rows are marked
  ``sent`` only after the uploader confirms a qos1 PUBACK / HTTP 200; a failed
  upload leaves them pending for the next drain with exponential backoff.
* Clock discipline (§5.4): rows carry ``ts_uncertain`` — set while the node has
  not yet observed an NTP sync. :meth:`heal_uncertain_clock` re-bases uncertain
  rows once wall-clock time is trusted; whatever is still uncertain at upload
  time rides with ``tsUncertain: true`` and the server stamps receive-time.

This module owns *only* the buffer + batch shaping. The actual network send +
backoff loop lives in :mod:`agal_one_agent.telemetry_uploader`, which calls
:meth:`fetch_batch` / :meth:`mark_sent`. Both are import-safe on any box: the
only dependency is the stdlib ``sqlite3``.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Default on-node DB location. OTA/rollback must preserve this directory
#: (ADR-013 §10 "Harder") — it lives outside the git-checkout install dir.
DEFAULT_DB_PATH = "/var/lib/agal-one-agent/telemetry.db"

#: Ring-buffer defaults (overridable via TelemetryBatchConfig / config.yaml).
DEFAULT_RETENTION_HOURS = 48      # ADR-013 §5.1: ">= 48 h at the finest tier"
DEFAULT_MAX_ROWS = 360_000        # ~7 days @ 10 s × ~6 ports (hard cap)
DEFAULT_MAX_BYTES = 64 * 1024 * 1024  # 64 MB hard cap (§5.1)
DEFAULT_MAX_BATCH_READINGS = 500  # <=500 readings per telemetry_batch (§5.2)
DEFAULT_MAX_BATCH_BYTES = 256 * 1024  # <=256 KB body per batch (§5.3/§6.2)

#: Per-tier drain priority — raw-tier ports drain before basic so the most
#: expensive data lands first (ADR-013 §3 "Buffer replay priority", §5.3).
#: Higher number = drained earlier.
TIER_PRIORITY = {
    "raw": 3,
    "fine": 2,
    "standard": 2,
    "basic": 1,
}
DEFAULT_PRIORITY = 2  # unknown / untiered ports drain at "normal"


@dataclass
class TelemetryBatchConfig:
    """Buffer + batch tunables (ADR-013 §5.7 ``telemetry.batch{...}``)."""

    flush_interval_sec: int = 900     # default 15-min stored cadence window
    max_readings: int = DEFAULT_MAX_BATCH_READINGS
    buffer_max_mb: int = 64
    retention_hours: int = DEFAULT_RETENTION_HOURS
    max_rows: int = DEFAULT_MAX_ROWS

    @property
    def buffer_max_bytes(self) -> int:
        return self.buffer_max_mb * 1024 * 1024

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "TelemetryBatchConfig":
        data = data or {}
        return cls(
            flush_interval_sec=int(data.get("flush_interval_sec", 900)),
            max_readings=int(data.get("max_readings", DEFAULT_MAX_BATCH_READINGS)),
            buffer_max_mb=int(data.get("buffer_max_mb", 64)),
            retention_hours=int(data.get("retention_hours", DEFAULT_RETENTION_HOURS)),
            max_rows=int(data.get("max_rows", DEFAULT_MAX_ROWS)),
        )


@dataclass
class BufferedBatch:
    """A drained batch ready for upload (shape aligns with the ingress
    ``telemetry_batch`` contract, ADR-013 §6.2)."""

    batch_id: str
    boot_session_id: str
    seqs: list[int]          # buffer PKs — passed back to mark_sent()
    readings: list[dict]     # {sourceKey, value, tsMs, tsUncertain?, kind?, unit?, quality?, win?, seq}

    def __len__(self) -> int:
        return len(self.readings)


class TelemetryBuffer:
    """SQLite-backed durable ring buffer for telemetry readings.

    Thread-safe: a single connection guarded by a lock (the sampling thread
    appends; the uploader thread drains/prunes). ``check_same_thread=False`` is
    safe because every access holds ``self._lock``.
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS readings (
        seq             INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_ms           INTEGER NOT NULL,
        ts_uncertain    INTEGER NOT NULL DEFAULT 0,
        port_key        TEXT    NOT NULL,
        kind_uri        TEXT,
        value           REAL,
        unit            TEXT,
        quality         TEXT,
        win_json        TEXT,
        priority        INTEGER NOT NULL DEFAULT 2,
        batch_id        TEXT,
        boot_session_id TEXT,
        sent            INTEGER NOT NULL DEFAULT 0
    );
    """
    # Drain order = highest priority first, then oldest seq first.
    _IDX = "CREATE INDEX IF NOT EXISTS ix_drain ON readings(sent, priority DESC, seq ASC);"

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH,
        config: Optional[TelemetryBatchConfig] = None,
        boot_session_id: Optional[str] = None,
        on_overflow: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.config = config or TelemetryBatchConfig()
        self.boot_session_id = boot_session_id or uuid.uuid4().hex[:16]
        # Called once (per run) with a buffer_overflow sensor_event dict when the
        # ring first drops data — wired to mqtt_client.publish_event in main.py.
        self._on_overflow = on_overflow
        self._overflow_reported = False
        self._lock = threading.Lock()

        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        # WAL: one fsync per commit (batch), not per row — SD-card wear (§5.1).
        # NORMAL sync is the documented safe pairing with WAL; a crash can lose
        # the last un-checkpointed commit, which is acceptable (§10 "buffer loss
        # != data loss once uploaded").
        try:
            self._db.execute("PRAGMA journal_mode=WAL;")
            self._db.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.Error as e:  # pragma: no cover — pragma always available
            logger.warning("Could not set WAL pragmas: %s", e)
        self._db.execute(self._DDL)
        self._db.execute(self._IDX)
        self._db.commit()

    # ---- Append -------------------------------------------------------------

    def append(
        self,
        readings: list[dict],
        ts_ms: Optional[int] = None,
        ts_uncertain: bool = False,
        priority: int = DEFAULT_PRIORITY,
    ) -> int:
        """Append a sampling cycle's readings to the buffer.

        ``readings`` are the same dicts the live channel publishes
        (``{sourceKey, value, kind?, unit?, quality?, ...extra}``). Extra keys
        beyond the known columns are preserved in ``win_json`` so nothing the
        sensor emitted (e.g. ``samples``/``windowMs``, per-subsystem calib) is
        lost on the durable path.

        Returns the number of rows appended. Runs a prune pass afterwards so the
        ring stays bounded.
        """
        if not readings:
            return 0
        stamp = int(ts_ms if ts_ms is not None else time.time() * 1000)
        rows = []
        for r in readings:
            known = {"sourceKey", "value", "kind", "kindUri", "unit", "quality"}
            extra = {k: v for k, v in r.items() if k not in known}
            rows.append((
                stamp,
                1 if ts_uncertain else 0,
                r.get("sourceKey", ""),
                r.get("kindUri") or r.get("kind"),
                _coerce_float(r.get("value")),
                r.get("unit"),
                r.get("quality"),
                json.dumps(extra) if extra else None,
                int(priority),
                self.boot_session_id,
            ))
        with self._lock:
            self._db.executemany(
                "INSERT INTO readings "
                "(ts_ms, ts_uncertain, port_key, kind_uri, value, unit, quality, "
                " win_json, priority, boot_session_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            self._db.commit()
            self._prune_locked()
        return len(rows)

    # ---- Drain --------------------------------------------------------------

    def fetch_batch(
        self,
        max_readings: Optional[int] = None,
        max_bytes: int = DEFAULT_MAX_BATCH_BYTES,
    ) -> Optional[BufferedBatch]:
        """Fetch the next batch of unsent rows, oldest-first within priority.

        Assigns a fresh ``batchId`` and stamps it on the drained rows (so the
        server can dedupe metering counters on qos1 replay — §5.5). Returns
        ``None`` when there is nothing pending. Does NOT mark rows sent — the
        uploader calls :meth:`mark_sent` only after the network confirms.
        """
        limit = max_readings or self.config.max_readings
        batch_id = uuid.uuid4().hex
        with self._lock:
            cur = self._db.execute(
                "SELECT seq, ts_ms, ts_uncertain, port_key, kind_uri, value, "
                "       unit, quality, win_json, boot_session_id "
                "FROM readings WHERE sent = 0 "
                "ORDER BY priority DESC, seq ASC LIMIT ?",
                (limit,),
            )
            fetched = cur.fetchall()
            if not fetched:
                return None

            seqs: list[int] = []
            readings: list[dict] = []
            approx_bytes = 0
            boot_session_id = self.boot_session_id
            for row in fetched:
                (seq, ts_ms, ts_uncertain, port_key, kind_uri, value,
                 unit, quality, win_json, row_boot) = row
                reading: dict = {
                    "sourceKey": port_key,
                    "value": value,
                    "tsMs": ts_ms,
                    "seq": seq,
                }
                if ts_uncertain:
                    reading["tsUncertain"] = True
                if kind_uri:
                    # Emit both spellings — ingress accepts kindUri|kind (§6.2).
                    reading["kindUri"] = kind_uri
                if unit:
                    reading["unit"] = unit
                if quality:
                    reading["quality"] = quality
                if win_json:
                    try:
                        reading["win"] = json.loads(win_json)
                    except json.JSONDecodeError:
                        pass
                if row_boot:
                    boot_session_id = row_boot

                enc = len(json.dumps(reading))
                # Keep at least one reading even if a single row exceeds the cap.
                if readings and approx_bytes + enc > max_bytes:
                    break
                approx_bytes += enc
                seqs.append(seq)
                readings.append(reading)

            # Stamp batch_id on exactly the rows we're returning.
            self._db.executemany(
                "UPDATE readings SET batch_id = ? WHERE seq = ?",
                [(batch_id, s) for s in seqs],
            )
            self._db.commit()

        return BufferedBatch(
            batch_id=batch_id,
            boot_session_id=boot_session_id,
            seqs=seqs,
            readings=readings,
        )

    def mark_sent(self, seqs: list[int]) -> int:
        """Mark rows as delivered (called on qos1 PUBACK / HTTP 200), then prune
        the now-sent rows past the retention window (on-success prune, §5.2)."""
        if not seqs:
            return 0
        with self._lock:
            placeholders = ",".join("?" * len(seqs))
            self._db.execute(
                f"UPDATE readings SET sent = 1 WHERE seq IN ({placeholders})",
                seqs,
            )
            self._db.commit()
            self._prune_locked()
            return len(seqs)

    # ---- Clock discipline (§5.4) -------------------------------------------

    def heal_uncertain_clock(self, boot_wall_ms: int, boot_monotonic_ms: int,
                             now_wall_ms: Optional[int] = None,
                             now_monotonic_ms: Optional[int] = None) -> int:
        """Re-base timestamps of rows written before NTP sync.

        Uncertain rows were stamped with a best-effort pre-NTP wall clock. Once
        the wall clock is trusted we can recover their true time from the
        monotonic delta: ``true_wall = boot_wall + (row_monotonic - boot_mono)``.
        We don't store per-row monotonic, so we approximate by shifting every
        uncertain row by the observed wall-clock correction (the jump detected at
        NTP sync). Rows are then cleared of the uncertain flag. Returns the
        number of rows healed.

        This is the "a >30 s wall-clock jump heals earlier rows by re-basing
        from monotonic deltas" path in §5.4; unhealed rows keep ``ts_uncertain``
        and upload with ``tsUncertain: true``.
        """
        now_wall = int(now_wall_ms if now_wall_ms is not None else time.time() * 1000)
        now_mono = int(now_monotonic_ms if now_monotonic_ms is not None
                       else time.monotonic() * 1000)
        # Correction = (true now) - (what the pre-NTP clock would have read now).
        pre_ntp_now = boot_wall_ms + (now_mono - boot_monotonic_ms)
        correction = now_wall - pre_ntp_now
        with self._lock:
            cur = self._db.execute(
                "UPDATE readings SET ts_ms = ts_ms + ?, ts_uncertain = 0 "
                "WHERE ts_uncertain = 1 AND sent = 0",
                (correction,),
            )
            self._db.commit()
            return cur.rowcount

    # ---- Introspection / health --------------------------------------------

    def pending_count(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COUNT(*) FROM readings WHERE sent = 0"
            ).fetchone()[0]

    def total_count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]

    def oldest_pending_ts(self) -> Optional[int]:
        with self._lock:
            row = self._db.execute(
                "SELECT MIN(ts_ms) FROM readings WHERE sent = 0"
            ).fetchone()
            return row[0] if row and row[0] is not None else None

    def health(self) -> dict:
        """Buffer-health snapshot for the status heartbeat (§8 contract item 6:
        ``buffer{pendingReadings, oldestPendingTs, overflowedAt?}``)."""
        h = {
            "pendingReadings": self.pending_count(),
            "oldestPendingTs": self.oldest_pending_ts(),
        }
        if self._overflow_reported:
            h["overflowed"] = True
        return h

    def close(self) -> None:
        with self._lock:
            try:
                self._db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            except sqlite3.Error:
                pass
            self._db.close()

    # ---- Internal -----------------------------------------------------------

    def _prune_locked(self) -> int:
        """Drop-oldest to keep the ring within bounds. Caller holds ``_lock``.

        Order of protection (§5.1):
          1. Never touch rows inside the retention window (newest N hours) unless
             a hard cap forces it.
          2. Prefer dropping already-``sent`` rows first.
          3. Hard caps (row count, byte size) drop the oldest rows regardless of
             sent state — with a one-time buffer_overflow event.
        """
        cfg = self.config
        dropped = 0

        # Retention floor: rows older than the window AND already sent are free
        # to reclaim.
        retention_cutoff = int(time.time() * 1000) - cfg.retention_hours * 3600 * 1000
        cur = self._db.execute(
            "DELETE FROM readings WHERE sent = 1 AND ts_ms < ?",
            (retention_cutoff,),
        )
        dropped += cur.rowcount

        # Hard row cap — drop oldest (sent-first via ORDER) if still over.
        total = self._db.execute("SELECT COUNT(*) FROM readings").fetchone()[0]
        if total > cfg.max_rows:
            overflow = total - cfg.max_rows
            self._db.execute(
                "DELETE FROM readings WHERE seq IN ("
                "  SELECT seq FROM readings ORDER BY sent DESC, seq ASC LIMIT ?)",
                (overflow,),
            )
            dropped += overflow
            self._note_overflow("max_rows", overflow)

        # Hard byte cap — page_count × page_size is the on-disk size proxy.
        page_count = self._db.execute("PRAGMA page_count;").fetchone()[0]
        page_size = self._db.execute("PRAGMA page_size;").fetchone()[0]
        if page_count * page_size > cfg.buffer_max_bytes:
            # Drop the oldest ~10% to get back under budget in one pass.
            n = max(1, self._db.execute(
                "SELECT COUNT(*) FROM readings").fetchone()[0] // 10)
            self._db.execute(
                "DELETE FROM readings WHERE seq IN ("
                "  SELECT seq FROM readings ORDER BY sent DESC, seq ASC LIMIT ?)",
                (n,),
            )
            dropped += n
            self._note_overflow("max_bytes", n)

        if dropped:
            self._db.commit()
        return dropped

    def _note_overflow(self, cap: str, count: int) -> None:
        """Emit a one-time buffer_overflow sensor_event on the first drop (§5.1)."""
        logger.warning("Telemetry buffer overflow (%s): dropped %d oldest rows", cap, count)
        if self._overflow_reported:
            return
        self._overflow_reported = True
        if self._on_overflow:
            try:
                self._on_overflow({
                    "type": "buffer_overflow",
                    "source": "node",
                    "sourceKey": "telemetry.buffer",
                    "payload": {"cap": cap, "droppedApprox": count},
                })
            except Exception as e:  # noqa: BLE001
                logger.debug("buffer_overflow event dispatch failed: %s", e)


def _coerce_float(value) -> Optional[float]:
    """Store numeric readings as REAL; leave non-numerics as NULL value (the
    sourceKey/win_json still carry them). Booleans map to 0/1."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
