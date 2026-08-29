"""Ring-buffer tests (ADR-013 P0) — mock mode, no hardware, in-memory SQLite.

Covers append / drain (telemetry_batch shape) / mark_sent+prune / overflow drop
+ one-time buffer_overflow event / clock heal / drain priority.
"""

from __future__ import annotations

import time

import pytest

from agal_one_agent.telemetry_buffer import (
    TelemetryBuffer,
    TelemetryBatchConfig,
    TIER_PRIORITY,
    _coerce_float,
)


def _buf(**cfg) -> TelemetryBuffer:
    return TelemetryBuffer(db_path=":memory:", config=TelemetryBatchConfig(**cfg))


# ----- append -----------------------------------------------------------------


def test_append_returns_row_count_and_persists():
    b = _buf()
    n = b.append([
        {"sourceKey": "pump.current", "value": 4.8, "unit": "A", "kind": "current"},
        {"sourceKey": "well.level", "value": 2.4, "unit": "m"},
    ])
    assert n == 2
    assert b.pending_count() == 2
    assert b.total_count() == 2


def test_append_empty_is_noop():
    b = _buf()
    assert b.append([]) == 0
    assert b.pending_count() == 0


def test_append_preserves_extra_keys_in_win_json():
    b = _buf()
    b.append([{"sourceKey": "imu.calib", "value": 3.0, "unit": "level",
               "sys": 3, "gyro": 2, "accel": 1, "mag": 0}])
    batch = b.fetch_batch()
    r = batch.readings[0]
    assert r["win"] == {"sys": 3, "gyro": 2, "accel": 1, "mag": 0}


# ----- drain (telemetry_batch shape) -----------------------------------------


def test_fetch_batch_shape_matches_ingress_contract():
    b = _buf()
    ts = int(time.time() * 1000)
    b.append([{"sourceKey": "pump.current", "value": 4.8, "unit": "A",
               "kind": "current", "quality": "good"}], ts_ms=ts)
    batch = b.fetch_batch()
    assert batch is not None
    assert len(batch.readings) == 1
    r = batch.readings[0]
    # ADR-013 §6.2 per-reading shape: sourceKey, value, tsMs, seq (+ optionals)
    assert r["sourceKey"] == "pump.current"
    assert r["value"] == 4.8
    assert r["tsMs"] == ts
    assert isinstance(r["seq"], int)
    assert r["unit"] == "A"
    assert r["kindUri"] == "current"      # kind emitted as kindUri (ingress accepts either)
    assert r["quality"] == "good"
    assert "tsUncertain" not in r          # certain clock ⇒ flag absent
    assert batch.batch_id and len(batch.batch_id) == 32   # uuid4 hex
    assert batch.boot_session_id


def test_fetch_batch_none_when_empty():
    assert _buf().fetch_batch() is None


def test_fetch_batch_respects_max_readings():
    b = _buf()
    b.append([{"sourceKey": f"p{i}", "value": i} for i in range(10)])
    batch = b.fetch_batch(max_readings=4)
    assert len(batch.readings) == 4


def test_fetch_batch_respects_byte_cap_but_keeps_one():
    b = _buf()
    b.append([{"sourceKey": "x" * 500, "value": 1.0}])
    batch = b.fetch_batch(max_bytes=10)   # smaller than one reading
    assert len(batch.readings) == 1        # always at least one


def test_ts_uncertain_flag_roundtrips():
    b = _buf()
    b.append([{"sourceKey": "s", "value": 1.0}], ts_uncertain=True)
    r = b.fetch_batch().readings[0]
    assert r["tsUncertain"] is True


# ----- mark_sent + prune ------------------------------------------------------


def test_mark_sent_clears_pending():
    b = _buf()
    b.append([{"sourceKey": "s", "value": 1.0}])
    batch = b.fetch_batch()
    assert b.pending_count() == 1
    b.mark_sent(batch.seqs)
    assert b.pending_count() == 0


def test_sent_rows_pruned_past_retention():
    # A row stamped well before the retention window, once sent, is reclaimed.
    b = _buf(retention_hours=1)
    old_ts = int(time.time() * 1000) - 2 * 3600 * 1000   # 2 h ago (> 1 h window)
    b.append([{"sourceKey": "s", "value": 1.0}], ts_ms=old_ts)
    batch = b.fetch_batch()
    b.mark_sent(batch.seqs)
    assert b.total_count() == 0   # sent + past-retention ⇒ reclaimed


def test_unsent_rows_survive_prune():
    b = _buf(retention_hours=0)
    old_ts = int(time.time() * 1000) - 24 * 3600 * 1000
    b.append([{"sourceKey": "s", "value": 1.0}], ts_ms=old_ts)
    # never marked sent — prune must not drop pending data even past retention
    b.append([{"sourceKey": "s2", "value": 2.0}], ts_ms=old_ts)
    assert b.pending_count() == 2


# ----- overflow (drop-oldest + one-time event) --------------------------------


def test_max_rows_overflow_drops_oldest_and_emits_one_event():
    events = []
    b = TelemetryBuffer(db_path=":memory:",
                        config=TelemetryBatchConfig(max_rows=5, retention_hours=999),
                        on_overflow=events.append)
    for i in range(8):
        b.append([{"sourceKey": f"p{i}", "value": float(i)}])
    # Hard cap enforced.
    assert b.total_count() <= 5
    # buffer_overflow event emitted exactly once (first drop of the run).
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == "buffer_overflow"
    assert ev["sourceKey"] == "telemetry.buffer"
    assert ev["source"] == "node"
    # Oldest dropped: the newest reading must still be present.
    batch = b.fetch_batch(max_readings=100)
    keys = [r["sourceKey"] for r in batch.readings]
    assert "p7" in keys
    assert "p0" not in keys


def test_overflow_event_not_repeated():
    events = []
    b = TelemetryBuffer(db_path=":memory:",
                        config=TelemetryBatchConfig(max_rows=3, retention_hours=999),
                        on_overflow=events.append)
    for i in range(20):
        b.append([{"sourceKey": f"p{i}", "value": float(i)}])
    assert len(events) == 1   # one-time per run


# ----- clock discipline -------------------------------------------------------


def test_heal_uncertain_clock_rebases_and_clears_flag():
    b = _buf()
    b.append([{"sourceKey": "s", "value": 1.0}], ts_ms=1000, ts_uncertain=True)
    # Simulate: node booted at wall=1000ms/mono=0ms; now wall=5000ms/mono=4000ms
    # ⇒ pre-NTP clock would read 1000+4000=5000 = actual ⇒ correction 0. Use a
    # skewed clock instead: real now is 60_000ms but pre-NTP thinks 5000ms.
    healed = b.heal_uncertain_clock(
        boot_wall_ms=1000, boot_monotonic_ms=0,
        now_wall_ms=60_000, now_monotonic_ms=4000,
    )
    assert healed == 1
    r = b.fetch_batch().readings[0]
    assert "tsUncertain" not in r          # flag cleared
    assert r["tsMs"] == 1000 + (60_000 - 5000)   # shifted by the correction


def test_heal_leaves_sent_rows_alone():
    b = _buf()
    b.append([{"sourceKey": "s", "value": 1.0}], ts_uncertain=True)
    batch = b.fetch_batch()
    b.mark_sent(batch.seqs)
    healed = b.heal_uncertain_clock(0, 0, now_wall_ms=100, now_monotonic_ms=0)
    assert healed == 0   # already-sent rows are not re-based


# ----- drain priority (raw before basic, §5.3) -------------------------------


def test_higher_priority_drains_first():
    b = _buf()
    b.append([{"sourceKey": "basic1", "value": 1.0}], priority=TIER_PRIORITY["basic"])
    b.append([{"sourceKey": "raw1", "value": 2.0}], priority=TIER_PRIORITY["raw"])
    batch = b.fetch_batch(max_readings=1)
    # raw (priority 3) drains before basic (priority 1) even though it was
    # appended later.
    assert batch.readings[0]["sourceKey"] == "raw1"


# ----- health -----------------------------------------------------------------


def test_health_snapshot():
    b = _buf()
    ts = int(time.time() * 1000)
    b.append([{"sourceKey": "s", "value": 1.0}], ts_ms=ts)
    h = b.health()
    assert h["pendingReadings"] == 1
    assert h["oldestPendingTs"] == ts


# ----- _coerce_float ----------------------------------------------------------


def test_coerce_float_handles_types():
    assert _coerce_float(1) == 1.0
    assert _coerce_float("2.5") == 2.5
    assert _coerce_float(True) == 1.0
    assert _coerce_float(False) == 0.0
    assert _coerce_float(None) is None
    assert _coerce_float("not-a-number") is None
